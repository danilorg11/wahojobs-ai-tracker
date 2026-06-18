import json
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from wahojobs.crawler.types import JobCandidate


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "application/json,text/markdown,text/plain,*/*",
    "Content-Type": "application/json",
    "Referer": "https://apply.workable.com/toloka-ai/",
}
PAGE_DELAY_SECONDS = 0.2
MAX_RETRIES = 3


def fetch_workable_jobs(api_url, account_slug):
    verify_public_markdown_feeds(account_slug)
    rows = fetch_all_api_rows(api_url)
    jobs = []
    for row in rows:
        candidate = parse_workable_row(account_slug, row)
        if candidate is not None:
            jobs.append(candidate)
    return jobs


def verify_public_markdown_feeds(account_slug):
    for path in ("llms.txt", "jobs.md"):
        url = f"https://apply.workable.com/{account_slug}/{path}"
        request = Request(url, headers={"User-Agent": REQUEST_HEADERS["User-Agent"]})
        with urlopen(request, timeout=30) as response:
            if response.status >= 400:
                raise RuntimeError(f"Workable {path} returned HTTP {response.status}")


def fetch_all_api_rows(api_url):
    rows = []
    token = None
    seen_tokens = set()

    while True:
        body = {"token": token} if token else {}
        data = fetch_api_page(api_url, body)
        page_rows = data.get("results") or []
        if not isinstance(page_rows, list):
            raise ValueError("Workable jobs response did not include a results list.")

        rows.extend(row for row in page_rows if isinstance(row, dict))

        token = data.get("nextPage")
        if not token or token in seen_tokens:
            break
        seen_tokens.add(token)
        time.sleep(PAGE_DELAY_SECONDS)

    return rows


def fetch_api_page(api_url, body):
    payload = None
    for attempt in range(1, MAX_RETRIES + 1):
        request = Request(
            api_url,
            data=json.dumps(body).encode("utf-8"),
            headers=REQUEST_HEADERS,
            method="POST",
        )
        try:
            with urlopen(request, timeout=45) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                payload = response.read().decode(charset, errors="replace")
            break
        except HTTPError as exc:
            if exc.code != 429 or attempt == MAX_RETRIES:
                raise
            time.sleep(2 * attempt)

    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Workable jobs response was not a JSON object.")
    return data


def parse_workable_row(account_slug, row):
    shortcode = clean_value(row.get("shortcode"))
    title = clean_value(row.get("title"))
    if not shortcode or not title:
        return None
    if row.get("state") != "published" or row.get("isInternal"):
        return None

    departments = [clean_value(value) for value in row.get("department") or []]
    departments = [value for value in departments if value]
    department = "; ".join(departments) if departments else "Unknown"
    location = format_location(row)
    commitment = format_commitment(row)

    return JobCandidate(
        external_id=shortcode,
        title=title,
        location=location,
        url=f"https://apply.workable.com/{account_slug}/j/{shortcode}",
        department=department,
        expertise=department,
        commitment=commitment,
    )


def format_location(row):
    location = row.get("location") or {}
    city = clean_value(location.get("city"))
    region = clean_value(location.get("region"))
    country = clean_value(location.get("country"))

    parts = []
    if city:
        parts.append(city)
    if region and region != city:
        parts.append(region)
    if country:
        parts.append(country)

    label = ", ".join(parts) if parts else "Remote"
    if row.get("remote") or row.get("workplace") == "remote":
        return f"{label} (Remote)"
    return label


def format_commitment(row):
    work_type = clean_value(row.get("type"))
    workplace = clean_value(row.get("workplace"))
    parts = []
    if work_type:
        parts.append({"part": "Part-time", "full": "Full-time"}.get(work_type, work_type))
    if workplace:
        parts.append(workplace.title())
    return "; ".join(parts) if parts else None


def clean_value(value):
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value or None
