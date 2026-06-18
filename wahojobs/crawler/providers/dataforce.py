import html
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from wahojobs.crawler.types import JobCandidate


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "text/html,application/xhtml+xml",
}
MAX_PAGES = 20


def fetch_dataforce_jobs(projects_url):
    jobs = []
    seen_external_ids = set()

    for page in range(MAX_PAGES):
        page_url = build_page_url(projects_url, page)
        html_text = fetch_page(page_url)
        page_jobs = parse_jobs_page(html_text, page_url)
        if not page_jobs:
            break

        new_jobs = []
        for job in page_jobs:
            if job.external_id in seen_external_ids:
                continue
            seen_external_ids.add(job.external_id)
            new_jobs.append(job)

        if not new_jobs:
            break
        jobs.extend(new_jobs)

    return jobs


def build_page_url(projects_url, page):
    if page == 0:
        return projects_url

    parsed = urlparse(projects_url)
    query = parse_qs(parsed.query)
    query["project_type"] = ["All"]
    query["page"] = [str(page)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def fetch_page(url):
    request = Request(url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=45) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def parse_jobs_page(html_text, page_url):
    jobs = []
    for block in html_text.split('<div class="views-row">')[1:]:
        job = parse_job_block(block, page_url)
        if job is not None:
            jobs.append(job)
    return jobs


def parse_job_block(block, page_url):
    title = extract_title(block)
    href = extract_project_href(block)
    if not title or not href:
        return None

    fields = extract_fields(block)
    category = normalize_category(fields.get("Category"))
    commitment = clean_value(fields.get("Type"))
    country = clean_value(fields.get("Country"))
    city = clean_value(fields.get("City"))
    location = build_location(country, city, commitment)
    absolute_url = urljoin(page_url, href)

    return JobCandidate(
        external_id=f"dataforce::{urlparse(absolute_url).path.strip('/')}",
        title=title,
        location=location,
        url=absolute_url,
        department=category,
        expertise=category,
        commitment=commitment,
    )


def extract_title(block):
    match = re.search(
        r'<div class="views-field views-field-title">\s*<h2 class="field-content">(.*?)</h2>',
        block,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    return clean_html_text(match.group(1))


def extract_project_href(block):
    match = re.search(
        r'href="(/(?:project|study)/[^"]+)"',
        block,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return html.unescape(match.group(1))


def extract_fields(block):
    fields = {}
    for label, value in re.findall(
        r"<strong>\s*(Category|Type|Country|City)\s*</strong>\s*<br>\s*(.*?)</p>",
        block,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        fields[clean_html_text(label)] = clean_html_text(value)
    return fields


def build_location(country, city, commitment):
    if city and country:
        return f"{city}, {country}"
    if country:
        return country
    if commitment and commitment.lower() == "remote":
        return "Remote"
    return "Unknown"


def normalize_category(category):
    value = clean_value(category)
    if not value:
        return "Unknown"

    normalized = value.lower()
    labels = {
        "audio": "Audio",
        "image": "Image",
        "photo": "Photo",
        "text": "Text",
        "video": "Video",
    }
    return labels.get(normalized, value)


def clean_html_text(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return clean_value(value)


def clean_value(value):
    if value is None:
        return None
    value = html.unescape(str(value)).replace("\xa0", " ")
    value = " ".join(value.split())
    return value or None
